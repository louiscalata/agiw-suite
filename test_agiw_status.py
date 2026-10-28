import datetime as dt
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import agiw_status


def snapshot():
    rows = []
    for host, platform in agiw_status.PLATFORMS.items():
        rows.append({'id': host, 'host': host, 'platform': platform, 'state': 'up',
                     'reasonCode': 'observed', 'trust': 'authenticated', 'ageSeconds': 0,
                     'modelsComplete': True, 'loadedCount': 1, 'busyCount': 1, 'handsOff': None,
                     'promptTokensPerSecond': None, 'generationTokensPerSecond': 4.5})
    return {'schemaVersion': 1, 'sampledAt': 1000, 'machines': rows, 'models': []}


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.endpoint = Path(self.temp.name) / 'endpoint.json'
        self.write_endpoint({'port': 43210, 'pid': 12, 'startedUnix': 900})

    def write_endpoint(self, data):
        self.endpoint.write_text(json.dumps(data))
        self.endpoint.chmod(0o600)

    def read(self, raw=None, status=200, headers=None, error=None):
        response = Mock()
        response.status = status
        response.getheader.side_effect = lambda key, default=None: (headers or {}).get(key, default)
        response.read.return_value = json.dumps(snapshot()).encode() if raw is None else raw
        connection = Mock()
        connection.getresponse.return_value = response
        if error is not None:
            connection.request.side_effect = error
        with patch.object(agiw_status.http.client, 'HTTPConnection', return_value=connection) as factory:
            result = agiw_status.summary(self.endpoint, now=1000)
        return result, factory, connection, response

    def assert_unknown(self, data, reason):
        self.assertEqual([row['host'] for row in data['machines']], ['mac', 'windows'])
        for row in data['machines']:
            self.assertEqual(row['state'], 'unknown')
            self.assertEqual(row['reasonCode'], reason)
            for key in ('loadedCount', 'busyCount', 'promptTokensPerSecond', 'generationTokensPerSecond'):
                self.assertIsNone(row[key])

    def trickle(self, *, header_delay=0, body_delay=0, connect_delay=0, send_delay=0,
                chunked=False):
        clock = [0.0]
        socket_timeout = [agiw_status.TIMEOUT]
        body = json.dumps(snapshot()).encode()
        if chunked:
            body = f'{len(body):x};padding='.encode() + b'x' * 30 + b'\r\n' + body + b'\r\n0\r\n\r\n'
            header = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
        else:
            header = f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n'.encode()
        raw = header + body

        class Stream(io.RawIOBase):
            offset = 0

            def readable(self):
                return True

            def readinto(self, buffer):
                if self.offset == len(raw):
                    return 0
                delay = header_delay if self.offset < len(header) else body_delay
                if delay > socket_timeout[0]:
                    clock[0] += socket_timeout[0]
                    raise TimeoutError()
                clock[0] += delay
                buffer[0] = raw[self.offset]
                self.offset += 1
                return 1

        sock = Mock()
        sock.settimeout.side_effect = lambda value: socket_timeout.__setitem__(0, value)
        sock.sendall.side_effect = lambda data: clock.__setitem__(0, clock[0] + send_delay)

        def makefile(mode, buffering=-1):
            stream = Stream()
            return stream if buffering == 0 else io.BufferedReader(stream)

        def connect(*args, **kwargs):
            clock[0] += connect_delay
            return sock

        sock.makefile.side_effect = makefile
        with patch.object(agiw_status.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(agiw_status.http.client.socket, 'create_connection', side_effect=connect):
            result = agiw_status.summary(self.endpoint, now=1000)
        return result, clock[0]

    def test_trickle_headers_stop_at_overall_deadline(self):
        result, elapsed = self.trickle(header_delay=0.1)
        self.assert_unknown(result, 'timeout')
        self.assertLessEqual(elapsed, agiw_status.TIMEOUT)

    def test_trickle_body_stops_at_overall_deadline(self):
        result, elapsed = self.trickle(body_delay=0.1)
        self.assert_unknown(result, 'timeout')
        self.assertLessEqual(elapsed, agiw_status.TIMEOUT)

    def test_trickle_chunk_framing_stops_at_overall_deadline(self):
        result, elapsed = self.trickle(body_delay=0.1, chunked=True)
        self.assert_unknown(result, 'timeout')
        self.assertLessEqual(elapsed, agiw_status.TIMEOUT)

    def test_connection_and_request_share_response_deadline(self):
        result, elapsed = self.trickle(connect_delay=1, send_delay=0.75, body_delay=0.01)
        self.assert_unknown(result, 'timeout')
        self.assertLessEqual(elapsed, agiw_status.TIMEOUT)

    def test_response_before_deadline_preserves_rows(self):
        result, elapsed = self.trickle(connect_delay=0.1, send_delay=0.1, body_delay=0.0001)
        self.assertEqual(result['machines'], snapshot()['machines'])
        self.assertLess(elapsed, agiw_status.TIMEOUT)

    def test_same_machine_rows_as_snapshot(self):
        result, factory, connection, response = self.read()
        self.assertEqual(result['machines'], snapshot()['machines'])
        self.assertNotIn('models', result)
        factory.assert_called_once_with('127.0.0.1', 43210, timeout=2.0)
        connection.request.assert_called_once_with('GET', '/api/snapshot', headers={'Accept': 'application/json'})
        response.read.assert_called_once_with(agiw_status.MAX_SNAPSHOT + 1)
        connection.close.assert_called_once()

    def test_preserves_reported_unknown_and_down(self):
        data = snapshot()
        data['machines'][1].update(state='down', reasonCode='reported_down', trust='share-reported')
        data['machines'][2].update(state='unknown', reasonCode='not_observed', ageSeconds=None,
                                   loadedCount=None, busyCount=None, generationTokensPerSecond=None)
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'], data['machines'])

    def test_missing_endpoint_does_not_contact_any_listener(self):
        self.endpoint.unlink()
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'unavailable')
        factory.assert_not_called()

    def test_endpoint_symlink_is_refused(self):
        other = self.endpoint.with_name('target.json')
        self.endpoint.rename(other)
        self.endpoint.symlink_to(other)
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'untrusted')
        factory.assert_not_called()

    def test_endpoint_directory_is_refused(self):
        self.endpoint.unlink()
        self.endpoint.mkdir()
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'untrusted')
        factory.assert_not_called()

    def test_endpoint_group_writable_is_refused(self):
        self.endpoint.chmod(0o620)
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'untrusted')
        factory.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO check')
    def test_endpoint_fifo_does_not_block(self):
        self.endpoint.unlink()
        os.mkfifo(self.endpoint, 0o600)
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'untrusted')
        factory.assert_not_called()

    def test_endpoint_over_size_is_not_parsed(self):
        self.endpoint.write_bytes(b' ' * (agiw_status.MAX_ENDPOINT + 1))
        result, factory, *_ = self.read()
        self.assert_unknown(result, 'oversize')
        factory.assert_not_called()

    def test_endpoint_port_must_be_integer_in_range(self):
        for port in (True, False, 0, -1, 65536, 1.5, '43210', None):
            with self.subTest(port=port):
                self.write_endpoint({'port': port})
                result, factory, *_ = self.read()
                self.assert_unknown(result, 'schema_invalid')
                factory.assert_not_called()

    def test_foreign_endpoint_host_is_refused(self):
        for host in ('localhost', '10.99.0.2', '127.0.0.1:43210', None):
            with self.subTest(host=host):
                self.write_endpoint({'port': 43210, 'host': host})
                result, factory, *_ = self.read()
                self.assert_unknown(result, 'untrusted')
                factory.assert_not_called()

    def test_endpoint_url_cannot_choose_a_destination(self):
        self.write_endpoint({'port': 43210, 'url': 'https://example.invalid/private'})
        result, factory, connection, _ = self.read()
        self.assertEqual(result['machines'], snapshot()['machines'])
        factory.assert_called_once_with('127.0.0.1', 43210, timeout=2.0)
        self.assertEqual(connection.request.call_args.args, ('GET', '/api/snapshot'))

    def test_endpoint_metadata_is_typed(self):
        for extra in ({'pid': True}, {'pid': -1}, {'startedUnix': False}, {'startedUnix': -1},
                      {'startedUnix': 10 ** 1000}):
            with self.subTest(extra=extra):
                self.write_endpoint({'port': 43210, **extra})
                result, factory, *_ = self.read()
                self.assert_unknown(result, 'schema_invalid')
                factory.assert_not_called()

    def test_connection_faults_have_named_unknown_reasons(self):
        for error, reason in ((TimeoutError(), 'timeout'), (ConnectionRefusedError(), 'refused'),
                              (PermissionError(), 'unavailable'), (OSError(), 'unavailable')):
            with self.subTest(reason=reason):
                result, _, connection, _ = self.read(error=error)
                self.assert_unknown(result, reason)
                connection.close.assert_called_once()

    def test_redirect_and_http_errors_are_not_followed(self):
        for status in (301, 302, 307, 404, 503):
            with self.subTest(status=status):
                result, factory, connection, response = self.read(
                    status=status, headers={'Location': 'https://example.invalid/'})
                self.assert_unknown(result, 'unavailable')
                factory.assert_called_once()
                connection.request.assert_called_once()
                response.read.assert_not_called()

    def test_json_errors_are_named_without_response_content(self):
        for raw in (b'<html>private response</html>', b'\xff', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1e999}'):
            with self.subTest(raw=raw[:20]):
                result, *_ = self.read(raw)
                self.assert_unknown(result, 'non_json')
                self.assertNotIn('private response', json.dumps(result))

    def test_json_depth_is_bounded(self):
        result, *_ = self.read(b'[' * 40 + b']' * 40)
        self.assert_unknown(result, 'schema_invalid')

    def test_response_size_is_bounded_with_and_without_length(self):
        result, _, _, response = self.read(headers={'Content-Length': str(agiw_status.MAX_SNAPSHOT + 1)})
        self.assert_unknown(result, 'oversize')
        response.read.assert_not_called()
        result, *_ = self.read(b' ' * (agiw_status.MAX_SNAPSHOT + 1))
        self.assert_unknown(result, 'oversize')

    def test_response_length_and_encoding_are_validated(self):
        for headers, reason in (({'Content-Length': 'x'}, 'schema_invalid'),
                                ({'Content-Length': '-2'}, 'schema_invalid'),
                                ({'Content-Encoding': 'gzip'}, 'non_json')):
            with self.subTest(headers=headers):
                result, _, _, response = self.read(headers=headers)
                self.assert_unknown(result, reason)
                response.read.assert_not_called()

    def test_invalid_snapshot_schema_is_unknown(self):
        for data in ([], {}, {'schemaVersion': True, 'sampledAt': 1000, 'machines': []},
                     {**snapshot(), 'sampledAt': True}, {**snapshot(), 'sampledAt': 10 ** 1000},
                     {**snapshot(), 'machines': []},
                     {**snapshot(), 'machines': [snapshot()['machines'][0]] * 3}):
            with self.subTest(data=data):
                result, *_ = self.read(json.dumps(data).encode())
                self.assert_unknown(result, 'schema_invalid')

    def test_stale_and_future_snapshot_are_unknown(self):
        stale, *_ = self.read(json.dumps({**snapshot(), 'sampledAt': 969}).encode())
        self.assertEqual([row['host'] for row in stale['machines']], ['mac', 'windows', 'linux'])
        for row in stale['machines']:
            self.assertEqual(row['state'], 'unknown')
            self.assertEqual(row['reasonCode'], 'stale')
            for key in ('loadedCount', 'busyCount', 'promptTokensPerSecond', 'generationTokensPerSecond'):
                self.assertIsNone(row[key])
        future, *_ = self.read(json.dumps({**snapshot(), 'sampledAt': 1002.1}).encode())
        self.assert_unknown(future, 'clock_skew')

    def test_served_machine_states_match_cli_after_mac_sample_expires(self):
        from server import SnapshotStore
        data = snapshot()
        data['sampledAt'] = 996
        store = SnapshotStore()
        store.publish(data)
        with patch('server.time.time', return_value=1000):
            served = store.read()
        self.assertEqual([row['state'] for row in served['machines']], ['unknown', 'up', 'up'])
        result, *_ = self.read(json.dumps(served).encode())
        self.assertEqual(result['machines'], served['machines'])
        self.assertEqual(result['sampledAt'], served['sampledAt'])

    def test_expired_peer_keeps_paired_identity_and_observation(self):
        data = snapshot()
        peer = data['machines'][1]
        peer.update(id='3d4c58e5-2df8-4c2f-a62e-f479e31f8831',
                    nodeId='3d4c58e5-2df8-4c2f-a62e-f479e31f8831',
                    observedAt=dt.datetime.fromtimestamp(969, dt.timezone.utc).isoformat())
        result, *_ = self.read(json.dumps(data).encode())
        expired = result['machines'][1]
        self.assertEqual((expired['state'], expired['reasonCode']), ('unknown', 'stale'))
        for key in ('id', 'nodeId', 'host', 'platform', 'trust', 'observedAt'):
            self.assertEqual(expired.get(key), peer[key])
        self.assertEqual(expired['ageSeconds'], 31)
        self.assertFalse(expired['modelsComplete'])
        for key in ('loadedCount', 'busyCount', 'promptTokensPerSecond', 'generationTokensPerSecond'):
            self.assertIsNone(expired[key])

    def test_per_host_expiry_ages_legacy_rows(self):
        data = snapshot()
        data['sampledAt'] = 999
        data['machines'][0]['ageSeconds'] = 2.1
        data['machines'][1]['ageSeconds'] = 28.9
        data['machines'][2]['ageSeconds'] = 29.1
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'][0]['reasonCode'], 'stale')
        self.assertEqual(result['machines'][1], {**data['machines'][1], 'ageSeconds': 29.9})
        self.assertEqual(result['machines'][2]['reasonCode'], 'stale')
        self.assertIsNone(result['machines'][0]['loadedCount'])

    def test_serving_aged_machine_does_not_add_snapshot_age_twice(self):
        data = snapshot()
        data['sampledAt'] = 998
        data['machines'][1].update(ageSeconds=29, observedAt=dt.datetime.fromtimestamp(
            971, dt.timezone.utc).isoformat())
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'][1], data['machines'][1])

    def test_observed_timestamp_controls_expiry_and_clock_skew(self):
        for observed, reason in ((969, 'stale'), (1003, 'clock_skew')):
            with self.subTest(observed=observed):
                data = snapshot()
                data['machines'][1]['observedAt'] = dt.datetime.fromtimestamp(
                    observed, dt.timezone.utc).isoformat()
                result, *_ = self.read(json.dumps(data).encode())
                self.assertEqual(result['machines'][1]['state'], 'unknown')
                self.assertEqual(result['machines'][1]['reasonCode'], reason)
                self.assertEqual(result['machines'][0], data['machines'][0])

    def test_invalid_observed_timestamp_cannot_claim_health(self):
        for observed in ('invalid', '1970-01-01T00:16:40', 1000):
            with self.subTest(observed=observed):
                data = snapshot()
                data['machines'][1]['observedAt'] = observed
                result, *_ = self.read(json.dumps(data).encode())
                self.assert_unknown(result, 'schema_invalid')

    def test_age_is_checked_after_the_http_read(self):
        current = [1000]

        def delayed_read(*args):
            current[0] = 1004
            return snapshot()

        with patch.object(agiw_status, '_snapshot', side_effect=delayed_read), \
                patch.object(agiw_status.time, 'time', side_effect=lambda: current[0]):
            result = agiw_status.summary(self.endpoint)
        self.assertEqual([row['state'] for row in result['machines']], ['unknown', 'up', 'up'])
        self.assertEqual(result['machines'][0]['reasonCode'], 'stale')
        self.assertIsNone(result['machines'][0]['loadedCount'])
        self.assertEqual([row['ageSeconds'] for row in result['machines']], [4, 4, 4])

    def test_aging_keeps_reported_unknown_reason_and_identity(self):
        data = snapshot()
        data['sampledAt'] = 996
        peer = data['machines'][2]
        peer.update(id='paired-linux', nodeId='paired-linux', state='unknown', reasonCode='timeout',
                    ageSeconds=40, modelsComplete=False, loadedCount=None, busyCount=None,
                    generationTokensPerSecond=None)
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'][2], {**peer, 'ageSeconds': 44})

    def test_real_machine_summary_rows_are_preserved(self):
        data = snapshot()
        data['machines'] = [agiw_status.machine_summary(host, platform, now=1000)
                            for host, platform in agiw_status.PLATFORMS.items()]
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'], data['machines'])

    def test_real_peer_hostnames_and_multiple_hosts_roundtrip(self):
        report = {'health': 'ok', 'ageSeconds': 0, 'modelsComplete': True, 'models': []}
        pairs = (('mac', 'mac'), ('winpc01', 'windows'), ('second-pc', 'windows'),
                 ('linux-node-1', 'linux'))
        data = snapshot()
        data['machines'] = [agiw_status.machine_summary(host, platform, report, now=1000,
                                                        trust='local' if host == 'mac' else 'authenticated')
                            for host, platform in pairs]
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual(result['machines'], data['machines'])

    def test_expired_peer_keeps_its_hostname_and_platform(self):
        data = snapshot()
        data['sampledAt'] = 999
        data['machines'][2].update(id='linux-node-1', host='linux-node-1', ageSeconds=30)
        result, *_ = self.read(json.dumps(data).encode())
        peer = result['machines'][2]
        self.assertEqual((peer['host'], peer['platform'], peer['state'], peer['reasonCode']),
                         ('linux-node-1', 'linux', 'unknown', 'stale'))

    def test_missing_required_platform_and_excess_hosts_are_invalid(self):
        missing = snapshot()
        missing['machines'] = [row for row in missing['machines'] if row['platform'] != 'windows']
        excess = snapshot()
        excess['machines'] += [{**excess['machines'][1], 'host': f'pc-{index}'}
                               for index in range(agiw_status.MAX_MACHINES)]
        for data in (missing, excess):
            with self.subTest(data=data):
                result, *_ = self.read(json.dumps(data).encode())
                self.assert_unknown(result, 'schema_invalid')

    def test_mac_and_windows_snapshot_does_not_require_linux(self):
        data = snapshot()
        data['machines'] = [row for row in data['machines'] if row['platform'] != 'linux']
        result, *_ = self.read(json.dumps(data).encode())
        self.assertEqual([row['platform'] for row in result['machines']], ['mac', 'windows'])
        self.assertTrue(all(row['state'] == 'up' for row in result['machines']))

    def test_machine_schema_and_numeric_types_are_validated(self):
        changes = ({'host': '../foreign'}, {'host': []}, {'state': 'idle'}, {'reasonCode': ''},
                   {'platform': 'linux'}, {'platform': []}, {'ageSeconds': True}, {'ageSeconds': -1},
                   {'ageSeconds': None}, {'loadedCount': True}, {'busyCount': -1}, {'busyCount': 1.5},
                   {'generationTokensPerSecond': True}, {'promptTokensPerSecond': '4'},
                   {'ageSeconds': 10 ** 1000}, {'loadedCount': 10 ** 1000}, {'trust': []})
        for change in changes:
            with self.subTest(change=change):
                data = snapshot()
                data['machines'][0].update(change)
                result, *_ = self.read(json.dumps(data).encode())
                self.assert_unknown(result, 'schema_invalid')

    def test_unverified_report_cannot_claim_up_or_down(self):
        for state in ('up', 'down'):
            with self.subTest(state=state):
                data = snapshot()
                data['machines'][0].update(state=state, trust='unverified')
                result, *_ = self.read(json.dumps(data).encode())
                self.assert_unknown(result, 'untrusted')

    def test_unknown_with_zero_counts_is_not_emitted(self):
        data = snapshot()
        data['machines'][0].update(state='unknown', reasonCode='timeout', loadedCount=0,
                                   busyCount=0, generationTokensPerSecond=0)
        result, *_ = self.read(json.dumps(data).encode())
        self.assert_unknown(result, 'schema_invalid')

    def test_json_cli_unknown_returns_success(self):
        self.endpoint.unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            code = agiw_status.main(['--json', '--endpoint', str(self.endpoint)])
        self.assertEqual(code, 0)
        self.assert_unknown(json.loads(output.getvalue()), 'unavailable')

    def test_text_cli_names_unknown_reason(self):
        self.endpoint.unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            code = agiw_status.main(['--endpoint', str(self.endpoint)])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), 'mac: unknown (unavailable)\nwindows: unknown (unavailable)\n')

    def test_cli_entrypoint_is_executable_and_only_reads_requested_fixture(self):
        self.endpoint.unlink()
        entry = Path(__file__).with_name('agiw-status')
        self.assertTrue(entry.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run([sys.executable, '-B', str(entry), '--json', '--endpoint', str(self.endpoint)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_unknown(json.loads(result.stdout), 'unavailable')

    def test_cli_malformed_arguments_keep_parser_error(self):
        with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                agiw_status.main(['--no-such-option'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
