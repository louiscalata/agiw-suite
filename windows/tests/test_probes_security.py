"""Security regressions using in-memory HTTP, native API, and status fixtures."""

import datetime as dt
import http.client
import io
import socket
import unittest
from pathlib import Path
from unittest import mock

from windows.agiw_win import probes

NOW = 1_700_000_000.0


def snapshot(slots_state="unavailable"):
    return {
        "sources": [
            {"id": source, "state": state, "ageSeconds": 0}
            for source, state in (("lane-inventory", "live"), ("lane-config", "live"), ("llama-slots", slots_state))
        ],
        "lanes": [{"id": "fast", "status": "idle", "slotsBusy": 0, "slotsTotal": 1, "slots": []}],
        "models": [
            {"id": "model-fast", "loaded": True, "ageSeconds": 0, "state": "loaded", "metadata": {"lane": "fast"}}
        ],
        "machines": [
            {
                "id": "pc",
                "role": "this-machine",
                "state": "up",
                "loadedCount": 1,
                "modelsComplete": True,
                "busyCount": 0,
                "promptTokensPerSecond": 12,
                "generationTokensPerSecond": 8,
            }
        ],
    }


def peer_document(count, age=0):
    observed = dt.datetime.fromtimestamp(NOW - age, dt.timezone.utc).isoformat()
    return {
        "schemaVersion": 1,
        "protocol": "agiw-peer/2",
        "observedAt": observed,
        "self": {"host": "windows", "node": "ffffffff-0000-4000-8000-000000000000"},
        "peers": [
            {
                "node": f"{index:08x}-0000-4000-8000-000000000000",
                "host": "mac",
                "name": f"peer-{index}",
                "paired": True,
                "state": "connected",
                "status": {
                    "schemaVersion": 1,
                    "host": "mac",
                    "observedAt": observed,
                    "models": [{"id": "model", "loadedState": "loaded"}],
                },
            }
            for index in range(count)
        ],
    }


def share_record(**updates):
    record = {
        "host": "mac",
        "platform": "macos",
        "fresh": True,
        "verified": False,
        "source": "mac.json",
        "observedUnix": NOW,
        "ageSeconds": 0,
        "health": "ok",
        "modelsComplete": True,
        "models": [{"loadedState": "loaded", "activity": "busy"}],
    }
    record.update(updates)
    return record


class DetachedResponseDeadline(unittest.TestCase):
    def check_deadline(self, headers):
        timer = mock.Mock()
        sock = mock.Mock()
        connection = http.client.HTTPConnection("127.0.0.1", timeout=0.025)
        connection._create_connection = mock.Mock(return_value=sock)
        detached_at_read = []

        class ResponseStream(io.BytesIO):
            def read(self, size=-1):
                detached_at_read.append(connection.sock is None)
                timer.callback()
                if sock.shutdown.called:
                    return b""
                return super().read(size)

            def readinto(self, buffer):
                detached_at_read.append(connection.sock is None)
                timer.callback()
                if sock.shutdown.called:
                    return 0
                return super().readinto(buffer)

        stream = ResponseStream(headers + b"{}")
        sock.makefile.return_value = stream

        def make_timer(interval, callback):
            self.assertEqual(interval, 0.05)
            timer.callback = callback
            return timer

        with (
            mock.patch.object(probes.http.client, "HTTPConnection", return_value=connection),
            mock.patch.object(probes.threading, "Timer", side_effect=make_timer),
            self.assertRaisesRegex(TimeoutError, "total deadline"),
        ):
            probes.http_get_json("http://127.0.0.1/status", timeout=0.025)
        self.assertEqual(detached_at_read, [True])
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        timer.cancel.assert_called_once_with()
        self.assertTrue(stream.closed)

    def test_http10_body_deadline_interrupts_detached_socket(self):
        self.check_deadline(b"HTTP/1.0 200 OK\r\n\r\n")

    def test_connection_close_body_deadline_interrupts_detached_socket(self):
        self.check_deadline(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\n")

    def test_detached_response_closed_after_success(self):
        response = mock.Mock(status=200)
        response.read.return_value = b"{}"
        connection = mock.Mock(sock=mock.Mock())
        connection.getresponse.return_value = response
        with (
            mock.patch.object(probes.http.client, "HTTPConnection", return_value=connection),
            mock.patch.object(probes.threading, "Timer"),
        ):
            self.assertEqual(probes.http_get_json("http://127.0.0.1/status"), {})
        response.close.assert_called_once_with()
        connection.close.assert_called_once_with()


class LegacyTimestampRegressions(unittest.TestCase):
    @staticmethod
    def read_legacy(observed_at, now, refused=None):
        report = {"schemaVersion": 1, "host": "mac", "health": "ok", "observedAt": observed_at, "models": []}
        with mock.patch.object(probes, "read_json", return_value=report):
            return probes._legacy_mac_status(Path("unused-share"), now, on_refusal=refused)

    def test_naive_timestamp_is_utc_even_when_local_clock_differs(self):
        class LocalDateTime(dt.datetime):
            def timestamp(self):
                if self.tzinfo is None:
                    return super().replace(tzinfo=dt.timezone.utc).timestamp() + 8 * 3600
                return super().timestamp()

        observed = dt.datetime(2026, 11, 16, 8, tzinfo=dt.timezone.utc).timestamp()
        with mock.patch.object(probes.dt, "datetime", LocalDateTime):
            peer = self.read_legacy("2026-11-16T08:00:00", observed + 10)
        self.assertIsNotNone(peer)
        self.assertEqual(peer["observedUnix"], observed)
        self.assertEqual(peer["ageSeconds"], 10)
        self.assertIs(peer["fresh"], True)

    def test_naive_timestamp_does_not_make_old_report_fresh(self):
        observed = dt.datetime(2026, 11, 16, 8, tzinfo=dt.timezone.utc).timestamp()
        peer = self.read_legacy("2026-11-16T08:00:00", observed + 8 * 3600)
        self.assertEqual(peer["observedUnix"], observed)
        self.assertIs(peer["fresh"], False)

    def test_aware_timestamp_retains_correct_age(self):
        observed = dt.datetime(2026, 11, 16, 8, tzinfo=dt.timezone.utc).timestamp()
        for stamp in ("2026-11-16T08:00:00Z", "2026-11-16T08:00:00+00:00", "2026-11-16T00:00:00-08:00"):
            with self.subTest(stamp=stamp):
                refused = mock.Mock()
                peer = self.read_legacy(stamp, observed + 10, refused)
                self.assertEqual(peer["observedUnix"], observed)
                self.assertEqual(peer["ageSeconds"], 10)
                self.assertIs(peer["fresh"], True)
                refused.assert_not_called()


class ProcessEnumerationRegressions(unittest.TestCase):
    def test_owner_refusal_preserves_public_exception(self):
        self.assertEqual(probes.OwnerUnverified.__name__, "OwnerUnverified")
        self.assertEqual(probes.OwnerUnverified.__qualname__, "OwnerUnverified")
        self.assertTrue(issubclass(probes.OwnerUnverified, RuntimeError))

    def native_api(self, succeeds):
        api = mock.Mock(psapi=mock.Mock(EnumProcesses=mock.Mock(return_value=succeeds)), kernel32=mock.Mock())
        for patcher in (
            mock.patch.object(probes, "IS_WINDOWS", True),
            mock.patch.object(probes.ctypes, "windll", api, create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return api

    @staticmethod
    def memory_status():
        return {
            "totalPhys": 100 * 2**30,
            "availPhys": 10 * 2**30,
            "commitLimit": 200 * 2**30,
            "commitAvail": 100 * 2**30,
        }

    def test_enum_failure_raises_instead_of_empty(self):
        api = self.native_api(False)
        with self.assertRaisesRegex(OSError, "EnumProcesses"):
            probes.top_processes()
        api.psapi.EnumProcesses.assert_called_once()
        api.kernel32.OpenProcess.assert_not_called()

    def test_enum_failure_keeps_consumers_unknown(self):
        api = self.native_api(False)
        block, source = probes.memory_block(status_fn=self.memory_status)
        self.assertEqual(block["consumers"], [])
        self.assertIs(block["consumersKnown"], False)
        self.assertEqual(block["level"], "tight")
        self.assertEqual(block["availablePercent"], 10)
        self.assertEqual(source["state"], "live")
        api.psapi.EnumProcesses.assert_called_once()

    def test_successful_empty_enumeration_remains_known(self):
        api = self.native_api(True)
        block, source = probes.memory_block(status_fn=self.memory_status)
        self.assertEqual(block["consumers"], [])
        self.assertIs(block["consumersKnown"], True)
        self.assertEqual(source["state"], "live")
        api.psapi.EnumProcesses.assert_called_once()
        api.kernel32.OpenProcess.assert_not_called()


class MachineActivityEvidence(unittest.TestCase):
    def test_missing_slots_clear_counts_but_preserve_residency(self):
        for state in ("unavailable", "error"):
            with self.subTest(state=state):
                original = snapshot(state)
                published = probes.enforce_snapshot_evidence(original)
                self.assertEqual(published["lanes"][0]["status"], "slots_unknown")
                machine = published["machines"][0]
                self.assertIsNone(machine["busyCount"])
                self.assertIsNone(machine["promptTokensPerSecond"])
                self.assertIsNone(machine["generationTokensPerSecond"])
                self.assertEqual(machine["loadedCount"], 1)
                self.assertIs(machine["modelsComplete"], True)
                self.assertEqual(machine["state"], "up")
                self.assertEqual(original["machines"][0]["busyCount"], 0)
                self.assertEqual(original["lanes"][0]["status"], "idle")

    def test_current_slots_preserve_known_zero(self):
        published = probes.enforce_snapshot_evidence(snapshot("live"))
        self.assertEqual(published["machines"][0]["busyCount"], 0)
        self.assertEqual(published["machines"][0]["loadedCount"], 1)
        self.assertEqual(published["lanes"][0]["status"], "idle")
        self.assertEqual(published["machines"][0]["promptTokensPerSecond"], 12)
        self.assertEqual(published["machines"][0]["generationTokensPerSecond"], 8)


class UnverifiedShareEvidence(unittest.TestCase):
    def test_unsigned_health_cannot_promote_machine(self):
        for health in ("ok", "degraded"):
            for verified in (False, None, "true"):
                with self.subTest(health=health, verified=verified):
                    row = probes._peer_machine(share_record(health=health, verified=verified), "macos", None)
                    self.assertEqual(row["state"], "unknown")
                    self.assertIn("unverified", row["reason"])
                    self.assertIsNone(row["loadedCount"])
                    self.assertIsNone(row["busyCount"])
                    self.assertFalse(row["modelsComplete"])
                    self.assertEqual(row["trust"], "share-reported")

    def test_stale_report_retains_stale_reason(self):
        row = probes._peer_machine(share_record(fresh=False, ageSeconds=300), "macos", None)
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["reason"], "stale")

    def test_negative_report_stays_down(self):
        row = probes._peer_machine(share_record(health="down"), "macos", None)
        self.assertEqual(row["state"], "down")
        self.assertIsNone(row["loadedCount"])


class PeerLinkCoverage(unittest.TestCase):
    @staticmethod
    def read(count, age=0):
        with mock.patch.object(probes, "_read_peer_link_file", return_value=peer_document(count, age)):
            return probes.read_peer_link(NOW, enabled=True)

    def test_truncated_list_qualifies_counts(self):
        rows, source = self.read(probes.PEER_LINK_MAX_ROWS + 1)
        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0]["state"], "connected")
        self.assertIsNotNone(rows[0]["status"])
        self.assertIs(source.get("truncated"), True)
        self.assertIs(source.get("complete"), False)
        self.assertEqual(source.get("omittedRows"), 1)
        self.assertEqual(source["state"], "unavailable")
        self.assertFalse(probes.source_current(source))
        self.assertIn("32 paired, 32 connected", source["detail"])
        self.assertIn("32 retained peers", source["detail"])
        self.assertIn("1 omitted", source["detail"])

    def test_stale_truncated_list_records_omissions(self):
        rows, source = self.read(probes.PEER_LINK_MAX_ROWS + 2, age=31)
        self.assertEqual(len(rows), 32)
        self.assertTrue(all(row["state"] == "unknown" for row in rows))
        self.assertIs(source.get("truncated"), True)
        self.assertIs(source.get("complete"), False)
        self.assertEqual(source.get("omittedRows"), 2)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("stale", source["detail"])
        self.assertIn("2 omitted", source["detail"])

    def test_complete_lists_keep_live_counts(self):
        for count in (0, probes.PEER_LINK_MAX_ROWS):
            with self.subTest(count=count):
                rows, source = self.read(count)
                self.assertEqual(len(rows), count)
                self.assertEqual(source["state"], "live")
                self.assertTrue(probes.source_current(source))
                self.assertFalse(source.get("truncated", False))
                self.assertEqual(
                    source["detail"],
                    f"Pairing service reports {count} paired, {count} connected "
                    "(local pairing service file; not independently verified)",
                )


if __name__ == "__main__":
    unittest.main()
