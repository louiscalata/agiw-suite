"""Windows memory parity with fake lane, process and system readings on every OS."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402

GIB = 2 ** 30
GENERIC_TIP = "Close apps you are not using before loading another model"
PAIR_TIP = ("Close apps you are not using before stopping a model lane "
            "(Nisi pair: PC edit routes stop while either lane is down)")
ROUTE_TIP = ("Close apps you are not using before stopping a model lane "
             "(a Nisi route is running: stopping a lane can stop it)")


def memory_status(available=8, commit_available=25):
    return {"totalPhys": 100 * GIB, "availPhys": available * GIB,
            "commitLimit": 100 * GIB, "commitAvail": commit_available * GIB}


def process(label="llama-server.exe", resident=GIB):
    return {"label": label, "residentBytes": resident, "processCount": 2, "gpuAllocBytes": None}


def lane_fetch(model, meta=None, slot_payload=None):
    def fetch(url, _timeout, _limit):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/v1/models"):
            return {"data": [{"id": model, "meta": meta}]}
        if url.endswith("/slots"):
            if isinstance(slot_payload, Exception):
                raise slot_payload
            return slot_payload if slot_payload is not None else [{"id": 0, "is_processing": False}]
        raise AssertionError("unexpected lane request")
    return fetch


class LaneModelBytes(unittest.TestCase):
    def test_safe_integer_boundaries_and_status_unchanged(self):
        spec = probes.DEFAULT_LANES[0]
        baseline = probes.probe_lane(spec, lane_fetch(spec["alias"]))
        for size in (1, 30 * GIB, 2 ** 52):
            with self.subTest(size=size):
                lane = probes.probe_lane(spec, lane_fetch(spec["alias"], {"size": size}))
                self.assertEqual(lane["modelBytes"], size)
                self.assertEqual({**lane, "modelBytes": None}, baseline)

    def test_missing_malformed_and_unsafe_sizes_are_unknown(self):
        spec = probes.DEFAULT_LANES[0]
        metas = [None, [], "size", {}] + [{"size": value} for value in
                 (None, True, False, 0, -1, 2 ** 52 + 1, 1.0, "123", [], {}, float("nan"), float("inf"))]
        for meta in metas:
            with self.subTest(meta=meta):
                lane = probes.probe_lane(spec, lane_fetch(spec["alias"], meta))
                self.assertIsNone(lane["modelBytes"])
                self.assertEqual(lane["status"], "idle")

    def test_size_is_retained_when_slots_or_declared_identity_differ(self):
        spec = probes.DEFAULT_LANES[0]
        for model, slot_payload, status in (
                (spec["alias"], OSError("slots unavailable"), "slots_unknown"),
                (spec["alias"], [{"id": 0, "is_processing": True}], "busy"),
                ("another-model", None, "identity_mismatch")):
            with self.subTest(status=status):
                lane = probes.probe_lane(spec, lane_fetch(model, {"size": 5 * GIB}, slot_payload))
                self.assertEqual((lane["status"], lane["modelBytes"]), (status, 5 * GIB))

    def test_no_unambiguous_model_means_unknown_size(self):
        spec = probes.DEFAULT_LANES[0]
        for payload, status in ((None, "no_identity"), ({"data": []}, "no_identity"),
                                ({"data": [None]}, "identity_mismatch"),
                                ({"data": [{"id": spec["alias"], "meta": {"size": GIB}}] * 2}, "identity_mismatch")):
            def fetch(url, _timeout, _limit):
                return {} if url.endswith("/health") else payload
            with self.subTest(payload=payload):
                lane = probes.probe_lane(spec, fetch)
                self.assertEqual(lane["status"], status)
                self.assertIsNone(lane["modelBytes"])
        lane = probes.probe_lane(spec, mock.Mock(side_effect=ConnectionRefusedError))
        self.assertEqual(lane["status"], "unreachable")
        self.assertIsNone(lane["modelBytes"])
        fetch = mock.Mock(side_effect=AssertionError("undeclared lane must not be probed"))
        self.assertIsNone(probes.probe_lane({**spec, "undeclared": True}, fetch)["modelBytes"])
        fetch.assert_not_called()


class MemoryParity(unittest.TestCase):
    def block(self, *, available=8, consumers=None, **context):
        return probes.memory_block(lambda: memory_status(available),
                                   lambda: [process()] if consumers is None else consumers, **context)[0]

    def test_server_labels_and_up_lane_totals(self):
        lanes = [{"status": status, "modelBytes": size * GIB}
                 for size, status in enumerate(("idle", "busy", "slots_unknown"), 1)]
        lanes += [{"status": status, "modelBytes": 50 * GIB} for status in
                  ("unknown", "stale", "loading", "unreachable", "identity_mismatch", "undeclared")]
        for label in ("llama-server.exe", "llama-server", "LLAMA-SERVER.EXE", "Llama-Server"):
            original = process(label)
            with self.subTest(label=label):
                row = self.block(consumers=[original], lanes=lanes)["consumers"][0]
                self.assertEqual(row, {**original, "label": "Local LLM server", "loadedModelCount": 3,
                                       "loadedModelBytes": 6 * GIB})
                self.assertEqual(original["label"], label)
                self.assertNotIn("loadedModelCount", original)
        for label in ("llama-server-helper.exe", "my-llama-server", "browser.exe"):
            original = process(label)
            self.assertEqual(self.block(consumers=[original], lanes=lanes)["consumers"], [original])

    def test_missing_size_keeps_count_but_omits_total(self):
        for size in (None, True, 0, -1, 1.0, "123", 2 ** 52 + 1):
            with self.subTest(size=size):
                row = self.block(lanes=[{"status": "idle", "modelBytes": GIB},
                                        {"status": "slots_unknown", "modelBytes": size}])["consumers"][0]
                self.assertEqual(row["loadedModelCount"], 2)
                self.assertNotIn("loadedModelBytes", row)
        row = self.block(lanes=[{"status": "busy"}])["consumers"][0]
        self.assertEqual(row["loadedModelCount"], 1)
        self.assertNotIn("loadedModelBytes", row)

    def test_server_row_is_kept_when_its_size_is_unknown_and_others_are_larger(self):
        # Without a trustworthy total the server ranks by its working set, but it is never cut (the Mac guard's
        # named-group rule); the dashboard then ranks the rows by its own weight.
        consumers = [process("a.exe", 4 * GIB), process("b.exe", 3 * GIB), process("c.exe", 2 * GIB), process(),
                     process("d.exe", GIB // 2)]
        block = self.block(consumers=consumers, lanes=[{"status": "idle", "modelBytes": 2 ** 52},
                                                       {"status": "busy", "modelBytes": 2 ** 52}])
        self.assertEqual([row["label"] for row in block["consumers"]], ["a.exe", "b.exe", "c.exe", "Local LLM server"])
        self.assertEqual(block["consumers"][-1]["loadedModelCount"], 2)
        self.assertNotIn("loadedModelBytes", block["consumers"][-1])

    def test_aggregate_past_the_dashboard_byte_bound_keeps_the_row(self):
        # Each lane is within bounds, but the sum is not: the row and its count stay, the total is omitted.
        row = self.block(lanes=[{"status": "idle", "modelBytes": 2 ** 52},
                                {"status": "busy", "modelBytes": 2 ** 52}])["consumers"][0]
        self.assertEqual((row["label"], row["loadedModelCount"]), ("Local LLM server", 2))
        self.assertNotIn("loadedModelBytes", row)
        row = self.block(lanes=[{"status": "idle", "modelBytes": 2 ** 51},
                                {"status": "busy", "modelBytes": 2 ** 51}])["consumers"][0]
        self.assertEqual(row["loadedModelBytes"], 2 ** 52)

    def test_no_up_lanes_adds_neither_field(self):
        for lanes in (None, [], [{"status": "stale", "modelBytes": GIB}]):
            row = self.block(lanes=lanes)["consumers"][0]
            self.assertEqual(row["label"], "Local LLM server")
            self.assertNotIn("loadedModelCount", row)
            self.assertNotIn("loadedModelBytes", row)

    def test_eight_rows_are_decorated_before_top_three_are_selected(self):
        rows = [process(f"app-{i}.exe", (9 - i) * GIB) for i in range(7)] + [process()]
        with mock.patch.object(probes, "top_processes", return_value=rows) as read:
            block, _ = probes.memory_block(lambda: memory_status(), lanes=[{"status": "idle", "modelBytes": 30 * GIB}])
        read.assert_called_once_with(limit=8)
        self.assertEqual([row["label"] for row in block["consumers"]], ["Local LLM server", "app-0.exe", "app-1.exe", "app-2.exe"])
        self.assertEqual(block["consumers"][0]["residentBytes"], GIB)
        self.assertEqual(rows[-1]["label"], "llama-server.exe")

    def test_ranking_uses_larger_of_residency_and_model_size(self):
        rows = [process("app.exe", 9 * GIB), process(resident=15 * GIB), process("other.exe", 8 * GIB)]
        block = self.block(consumers=rows, lanes=[{"status": "idle", "modelBytes": GIB}])
        self.assertEqual(block["consumers"][0]["label"], "Local LLM server")

    def test_exact_tips_and_precedence_at_tight_and_critical(self):
        up = [{"status": "slots_unknown", "modelBytes": GIB}]
        for available, level in ((8, "tight"), (3, "critical")):
            for lanes, nisi, live, expected in (
                    (up, {"state": "ready"}, False, PAIR_TIP),
                    (up, {"state": "ready"}, True, ROUTE_TIP),
                    ([], {"state": "ready"}, True, ROUTE_TIP),
                    ([], {"state": "ready"}, False, GENERIC_TIP),
                    (up, {"state": "partial"}, False, GENERIC_TIP),
                    (up, None, False, GENERIC_TIP)):
                with self.subTest(available=available, lanes=lanes, nisi=nisi, live=live):
                    block = self.block(available=available, lanes=lanes, nisi=nisi, route_live=live)
                    self.assertEqual((block["level"], block["suggestions"]), (level, [expected]))
                    self.assertEqual(block["reasons"], [f"{available}% of 100 GB RAM available", "commit charge 75% of limit"])
                    self.assertEqual(block["paused"], [])

    def test_no_tips_at_ok_or_watch_and_commit_still_escalates(self):
        for available, level in ((30, "ok"), (15, "watch")):
            block = self.block(available=available, route_live=True)
            self.assertEqual((block["level"], block["suggestions"]), (level, []))
        with mock.patch.object(probes, "top_processes") as read:
            probes.memory_block(lambda: memory_status(30))
        read.assert_not_called()
        block, _ = probes.memory_block(lambda: memory_status(30, 3), lambda: [], route_live=True)
        self.assertEqual((block["level"], block["suggestions"]), ("tight", [ROUTE_TIP]))
        self.assertIn("commit charge 97% of limit", block["reasons"])

    def test_consumer_failure_keeps_memory_and_tip(self):
        block, source = probes.memory_block(lambda: memory_status(), mock.Mock(side_effect=OSError), route_live=True)
        self.assertEqual((block["level"], block["consumers"], block["suggestions"]), ("tight", [], [ROUTE_TIP]))
        self.assertEqual(source["state"], "live")

    def test_memory_thresholds_use_unrounded_percentages(self):
        for avail, commit_avail, level, available, commit in (
                (1996, 2500, "watch", 20.0, 75.0),
                (1196, 2500, "tight", 12.0, 75.0),
                (596, 2500, "critical", 6.0, 75.0),
                (3000, 504, "ok", 30.0, 95.0),
                (3000, 500, "tight", 30.0, 95.0)):
            with self.subTest(avail=avail, commit_avail=commit_avail):
                block, _ = probes.memory_block(lambda: {"totalPhys": 10000, "availPhys": avail,
                                                        "commitLimit": 10000, "commitAvail": commit_avail}, lambda: [])
                self.assertEqual((block["level"], block["availablePercent"], block["commitPercent"]),
                                 (level, available, commit))

    def test_boolean_byte_counts_are_not_memory_readings(self):
        for key in ("totalPhys", "availPhys", "commitLimit", "commitAvail"):
            for value in (True, False):
                with self.subTest(key=key, value=value):
                    raw = {**memory_status(), key: value}
                    block, source = probes.memory_block(lambda: raw, lambda: [])
                    if key in ("totalPhys", "availPhys"):
                        self.assertEqual((block["level"], source["state"]), ("unknown", "unavailable"))
                    else:
                        self.assertEqual((block["level"], block["commitPercent"], source["state"]), ("tight", None, "live"))


class CollectorFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        processes = mock.Mock()
        processes.sample.return_value = ({}, None, None)
        self.collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: 1000.0,
                                   gpu_fn=lambda: ([], None), adapters_fn=lambda: [], lane_processes=processes)
        self.addCleanup(self.collector.close)
        self.patch(self.collector, "_lane", side_effect=lambda spec, _now: {
            **probes.probe_lane(spec, lane_fetch(spec["alias"], {"size": 5 * GIB})), "ageSeconds": 0.0})
        self.patch(self.collector, "_jobs", return_value=({}, None))
        self.patch(probes, "lane_specs", return_value=([dict(spec) for spec in probes.DEFAULT_LANES], None))
        self.patch(probes, "share_health", return_value=({"state": "ready"}, None))
        self.patch(probes, "read_peers", return_value=([], None))
        self.patch(probes, "_memory_status", side_effect=memory_status)
        self.patch(probes, "top_processes", return_value=[process()])
        self.nisi = self.patch(probes, "nisi_component", return_value={"id": "nisi", "state": "ready"})
        self.queue = self.patch(probes, "route_queue", return_value=({"status": "idle"}, None))

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


class CollectorMemoryParity(CollectorFixture):
    def test_default_memory_receives_lanes_nisi_and_current_queue(self):
        for status in ("running", "claimed", "queued", "idle", "completed", None):
            with self.subTest(status=status):
                self.queue.return_value = ({"status": status}, None)
                snapshot = self.collector.collect()
                memory = snapshot["memory"]
                self.assertEqual(memory["suggestions"], [ROUTE_TIP if status == "running" else PAIR_TIP])
                row = memory["consumers"][0]
                self.assertEqual((row["loadedModelCount"], row["loadedModelBytes"]), (2, 10 * GIB))
                self.assertEqual([lane["modelBytes"] for lane in snapshot["lanes"]], [5 * GIB, 5 * GIB])
                self.assertEqual(next(source for source in snapshot["sources"] if source["id"] == "mem-guard")["state"], "live")

    def test_context_aware_injected_probe_runs_after_nisi_and_queue(self):
        seen = []
        def memory(*, lanes, nisi, route_live):
            self.nisi.assert_called_once()
            self.queue.assert_called_once()
            seen.append((lanes, nisi, route_live))
            return {"level": "watch"}, None
        self.collector.memory_fn = memory
        self.queue.return_value = ({"status": "claimed"}, None)
        snapshot = self.collector.collect()
        self.assertEqual(seen, [(snapshot["lanes"], snapshot["components"][0], False)])
        self.assertEqual(snapshot["memory"], {"level": "watch"})

    def test_no_argument_injected_probe_still_works(self):
        seen = []
        def memory():
            seen.append(True)
            return {"level": "watch"}, probes._source("mem-guard", "Memory", "live", "injected")
        self.collector.memory_fn = memory
        snapshot = self.collector.collect()
        self.assertEqual((seen, snapshot["memory"]), ([True], {"level": "watch"}))
        self.assertIn("injected", next(source for source in snapshot["sources"] if source["id"] == "mem-guard")["detail"])

    def test_probe_error_is_guarded_without_retry(self):
        seen = []
        def memory(*, lanes, nisi, route_live):
            seen.append(True)
            raise TypeError("private probe detail")
        self.collector.memory_fn = memory
        snapshot = self.collector.collect()
        self.assertEqual(seen, [True])
        self.assertEqual(snapshot["memory"], {"level": "unknown", "consumers": [], "suggestions": [], "reasons": [], "paused": []})
        source = next(source for source in snapshot["sources"] if source["id"] == "mem-guard")
        self.assertEqual((source["state"], source["detail"]), ("error", "Probe failed (TypeError)"))

    def test_missing_nisi_and_queue_keep_memory_available(self):
        self.nisi.side_effect = OSError
        self.queue.side_effect = OSError
        snapshot = self.collector.collect()
        self.assertEqual(snapshot["memory"]["suggestions"], [GENERIC_TIP])
        self.assertEqual(snapshot["memory"]["consumers"][0]["loadedModelCount"], 2)


if __name__ == "__main__":
    unittest.main()


class PeerModelsComplete(unittest.TestCase):
    """A peer row says whether its model list arrived whole, so a missing list is never read as zero loaded."""

    def row(self, **over):
        value = {"schemaVersion": 1, "kind": "agiw-peer", "host": "mac", "platform": "macos", "observedUnix": 1000.0,
                 "models": [{"id": "a", "loadedState": "loaded", "activity": "idle"}], **over}
        if "modelsTotal" not in value:
            value["modelsTotal"] = len(value["models"]) if isinstance(value["models"], list) else 0
        return probes._validate_peer(value, 1001.0)

    def test_whole_lists_are_complete(self):
        self.assertTrue(self.row()["modelsComplete"])
        self.assertTrue(self.row(models=[])["modelsComplete"])
        self.assertTrue(self.row(models=[{"id": f"m{i}", "loadedState": "inactive"} for i in range(32)])["modelsComplete"])

    def test_missing_dropped_or_cut_lists_are_incomplete(self):
        value = {"schemaVersion": 1, "kind": "agiw-peer", "host": "mac", "platform": "macos", "observedUnix": 1000.0}
        missing = probes._validate_peer(value, 1001.0)
        self.assertEqual((missing["models"], missing["modelsComplete"]), ([], False))
        for models in (None, False, 0, "model", {}):
            row = self.row(models=models)
            self.assertEqual((row["models"], row["modelsComplete"]), ([], False))
        for bad in ({"id": ""}, {"id": 7}, "model", None):
            self.assertFalse(self.row(models=[{"id": "a", "loadedState": "loaded"}, bad])["modelsComplete"])
        cut = self.row(models=[{"id": f"m{i}", "loadedState": "inactive"} for i in range(33)])
        self.assertEqual((len(cut["models"]), cut["modelsComplete"]), (32, False))

    def test_total_must_be_an_exact_matching_integer(self):
        for total in (None, True, False, 1.0, "1", 0, -1, 2, 33):
            with self.subTest(total=total):
                self.assertFalse(self.row(modelsTotal=total)["modelsComplete"])
        value = {"schemaVersion": 1, "kind": "agiw-peer", "host": "mac", "observedUnix": 1000.0,
                 "models": [{"id": "a"}]}
        self.assertFalse(probes._validate_peer(value, 1001.0)["modelsComplete"])

    def test_duplicate_ids_keep_the_first_row_and_make_the_list_incomplete(self):
        first = {"id": "same", "loadedState": "inactive", "activity": "unknown"}
        row = self.row(models=[first, {"id": "same", "loadedState": "loaded"}])
        self.assertEqual((row["models"], row["modelsComplete"]), ([first], False))
        prefix = "m" * 120
        row = self.row(models=[{"id": prefix + "first"}, {"id": prefix + "second"}])
        self.assertEqual((len(row["models"]), row["modelsComplete"]), (1, False))

    def test_invalid_residency_is_unknown_and_incomplete(self):
        for state in (None, True, False, 7, "loading", [], {}):
            with self.subTest(state=state):
                row = self.row(models=[{"id": "a", "loadedState": state}])
                self.assertEqual(row["models"], [{"id": "a", "loadedState": "unknown", "activity": "unknown"}])
                self.assertFalse(row["modelsComplete"])
        self.assertFalse(self.row(models=[{"id": "a"}])["modelsComplete"])
        self.assertTrue(self.row(models=[{"id": "a", "loadedState": "unknown"}])["modelsComplete"])

    def test_schema_and_freshness_require_canonical_values(self):
        for schema in (True, False, 1.0, "1", None, 2):
            with self.subTest(schema=schema):
                self.assertIsNone(self.row(schemaVersion=schema))
        for age, fresh in ((30.0, True), (30.049, False)):
            row = self.row(observedUnix=1001.0 - age)
            self.assertEqual((row["ageSeconds"], row["fresh"]), (30.0, fresh))



class LinkedMacCount(CollectorFixture):
    """The PC's own Mac view, built from a share link, counts loaded models only from a complete list."""

    def linked(self, complete):
        peer = {"host": "mac", "platform": "macos", "health": "ok", "fresh": True, "ageSeconds": 2.0,
                "source": "agiw-peers", "modelsComplete": complete,
                "models": [{"id": f"m{i}", "loadedState": "inactive", "activity": "unknown"} for i in range(3)]}
        probes.read_peers.return_value = ([peer], None)
        with mock.patch.object(probes, "configured_mac_name", return_value="mac"):
            return self.collector.collect()["macPeer"]

    def test_incomplete_list_has_no_loaded_count(self):
        complete = self.linked(True)
        self.assertEqual((complete["state"], complete["loadedCount"]), ("linked", 0))
        for flag in (False, None):
            with self.subTest(modelsComplete=flag):
                mac = self.linked(flag)
                self.assertEqual((mac["state"], mac["loadedCount"]), ("linked", None))



class ProcessGroups(unittest.TestCase):
    """Executable groups ignore case (as Windows names do) and never cut the local LLM server."""

    def test_spellings_merge_and_the_server_survives_the_limit(self):
        samples = [(f"app-{i}.exe", (20 - i) * GIB) for i in range(8)]
        samples += [("Chrome.exe", GIB), ("chrome.EXE", GIB), ("llama-server.exe", GIB // 2), ("LLAMA-SERVER.EXE", GIB // 2)]
        rows = probes.group_processes(samples, limit=8)
        labels = [row["label"] for row in rows]
        self.assertEqual(labels[:8], [f"app-{i}.exe" for i in range(8)])
        self.assertEqual(rows[-1], {"label": "llama-server.exe", "residentBytes": GIB, "processCount": 2, "gpuAllocBytes": None})
        self.assertNotIn("Chrome.exe", labels, "ninth place is cut; only the server is kept beyond the limit")
        merged = probes.group_processes([("Chrome.exe", GIB), ("chrome.EXE", GIB)], limit=3)
        self.assertEqual(merged, [{"label": "Chrome.exe", "residentBytes": 2 * GIB, "processCount": 2, "gpuAllocBytes": None}])

    def test_memory_block_claims_the_lane_total_once(self):
        consumers = [process("llama-server.exe", GIB), process("LLAMA-SERVER.EXE", GIB), process("a.exe", 3 * GIB)]
        block, _ = probes.memory_block(lambda: memory_status(), lambda: consumers,
                                       lanes=[{"status": "idle", "modelBytes": 30 * GIB}])
        servers = [row for row in block["consumers"] if row["label"] == "Local LLM server"]
        self.assertEqual(len(servers), 1)
        self.assertEqual((servers[0]["residentBytes"], servers[0]["processCount"], servers[0]["loadedModelBytes"]),
                         (2 * GIB, 4, 30 * GIB))

    def test_names_are_grouped_before_display_truncation(self):
        prefix = "x" * 80
        rows = probes.group_processes([(prefix + "one.exe", GIB), (prefix.upper() + "ONE.EXE", GIB),
                                       (prefix + "two.exe", GIB)], limit=3)
        self.assertEqual([(row["label"], row["residentBytes"], row["processCount"]) for row in rows],
                         [(prefix, 2 * GIB, 2), (prefix, GIB, 1)])

    def test_full_pid_buffer_retries_and_retains_the_server_beyond_the_prefix(self):
        capacities = []
        def enumerate_processes(pids, size, needed):
            capacity = len(pids._obj)
            capacities.append(capacity)
            count = min(capacity, 3)
            for index in range(count):
                pids._obj[index] = index + 1
            needed._obj.value = count * probes.ctypes.sizeof(probes.ctypes.c_ulong)
            return True
        def memory(handle, counters, _size):
            counters._obj.WorkingSetSize = (4 - handle) * GIB
            return True
        def name(handle, _flags, buffer, _size):
            buffer.value = "llama-server.exe" if handle == 3 else f"app-{handle}.exe"
            return True
        psapi = mock.Mock(EnumProcesses=enumerate_processes, GetProcessMemoryInfo=memory)
        kernel = mock.Mock(OpenProcess=lambda _query, _inherit, pid: pid, QueryFullProcessImageNameW=name)
        with mock.patch.object(probes, "IS_WINDOWS", True), \
                mock.patch.object(probes.ctypes, "windll", mock.Mock(psapi=psapi, kernel32=kernel), create=True):
            rows = probes.top_processes(limit=1, max_pids=2)
        self.assertEqual(capacities, [2, 4])
        self.assertEqual([row["label"] for row in rows], ["app-1.exe", "llama-server.exe"])
        self.assertEqual(kernel.CloseHandle.call_count, 3)

    def test_pid_buffer_growth_stops_at_65536(self):
        capacities = []
        def enumerate_processes(pids, size, needed):
            capacities.append(len(pids._obj))
            needed._obj.value = size
            return True
        psapi = mock.Mock(EnumProcesses=enumerate_processes)
        with mock.patch.object(probes, "IS_WINDOWS", True), \
                mock.patch.object(probes.ctypes, "windll", mock.Mock(psapi=psapi), create=True):
            self.assertEqual(probes.top_processes(), [])
        self.assertEqual(capacities, [4096, 8192, 16384, 32768, 65536])

    def test_consumer_named_local_llm_server_does_not_become_the_server_row(self):
        ordinary = process("Local LLM server", 4 * GIB)
        rows = [ordinary, process("a.exe", 5 * GIB), process("b.exe", 3 * GIB), process("c.exe", 2 * GIB),
                process("llama-server.exe", GIB)]
        block, _ = probes.memory_block(lambda: memory_status(), lambda: rows,
                                       lanes=[{"status": "idle", "modelBytes": 30 * GIB}])
        self.assertEqual(len(block["consumers"]), 4)
        self.assertEqual(block["consumers"][0]["loadedModelBytes"], 30 * GIB)
        self.assertEqual(block["consumers"][0]["residentBytes"], GIB)
        self.assertIn(ordinary, block["consumers"])
        self.assertEqual(sum("loadedModelCount" in row for row in block["consumers"]), 1)
        self.assertNotIn("c.exe", [row["label"] for row in block["consumers"]])


class ConfiguredMacLink(CollectorFixture):
    """The share link stands in only for the configured Mac."""

    def run_with(self, name, hosts):
        peers = [{"host": host, "platform": "macos", "health": "ok", "fresh": True, "ageSeconds": 2.0,
                  "source": "agiw-peers", "modelsComplete": True,
                  "models": [{"id": f"{host}-model", "loadedState": "loaded", "activity": "idle"}]} for host in hosts]
        probes.read_peers.return_value = (peers, None)
        lan = {"state": "unreachable", "reason": "timeout", "models": [], "ageSeconds": 1.0, "observedAt": 1000.0}
        # Identity comes only from the current hosts.json (a LAN result's cached name can predate a config change).
        hosts = self.root / "llm-lab" / "mac-reach" / "hosts.json"
        if name is None:
            hosts.unlink(missing_ok=True)
        else:
            hosts.parent.mkdir(parents=True, exist_ok=True)
            hosts.write_text(json.dumps({"mac": {"hostname": name}}), encoding="utf-8")
            lan["name"] = name  # as probe_mac names its result from hosts.json's hostname
        self.collector.mac_peer = mock.Mock(read=mock.Mock(return_value=(lan, probes._source("mac-peer", "Mac (LAN)", "unavailable", "x"))))
        return self.collector.collect()["macPeer"]

    def test_link_follows_the_configured_name(self):
        mac = self.run_with("Target-Mac", ["other-mac", "target-mac"])
        self.assertEqual((mac["state"], [m["id"] for m in mac["models"]]), ("linked", ["target-mac-model"]))
        self.assertEqual(self.run_with("target-mac.local", ["target-mac"])["state"], "linked")
        self.assertEqual(self.run_with("target-mac", ["other-mac"])["state"], "unreachable")

    def test_a_current_lan_failure_stays_beside_the_share_link(self):
        mac = self.run_with("target-mac", ["target-mac"])
        self.assertEqual((mac["state"], mac["lanFailure"], mac["lanFailureAgeSeconds"], mac["ageSeconds"]),
                         ("linked", "timeout", 1.0, 2.0))
        # A pending or unknown LAN result is no failure.
        probes.read_peers.return_value = ([{"host": "target-mac", "platform": "macos", "health": "ok", "fresh": True,
                                            "ageSeconds": 2.0, "source": "agiw-peers", "modelsComplete": True,
                                            "models": []}], None)
        unknown = {"state": "unknown", "name": "target-mac", "models": [], "ageSeconds": None}
        self.collector.mac_peer = mock.Mock(read=mock.Mock(return_value=(unknown, probes._source("mac-peer", "Mac (LAN)", "unavailable", "x"))))
        mac = self.collector.collect()["macPeer"]
        self.assertEqual((mac["state"], mac["lanFailure"], mac["lanFailureAgeSeconds"]), ("linked", None, None))

    def test_a_previous_macs_lan_failure_never_follows_a_config_change(self):
        # hosts.json now names target-mac; the LAN cache still holds old-mac's timeout (Astra merge review P2).
        self.run_with("target-mac", ["target-mac"])
        old = {"state": "unreachable", "reason": "timeout", "name": "old-mac", "models": [], "ageSeconds": 1.0,
               "observedAt": 1000.0}
        self.collector.mac_peer = mock.Mock(read=mock.Mock(return_value=(old, probes._source("mac-peer", "Mac (LAN)", "unavailable", "x"))))
        mac = self.collector.collect()["macPeer"]
        self.assertEqual((mac["state"], mac["lanFailure"], mac["lanFailureAgeSeconds"], mac["configuredName"]),
                         ("linked", None, None, "target-mac"))
        # A nameless cached failure (an mDNS-only config: the probe names its result only from hostname) proves no
        # identity, so it is never attached to the configured Mac either.
        nameless = {k: v for k, v in old.items() if k != "name"}
        self.collector.mac_peer = mock.Mock(read=mock.Mock(return_value=(nameless, probes._source("mac-peer", "Mac (LAN)", "unavailable", "x"))))
        mac = self.collector.collect()["macPeer"]
        self.assertEqual((mac["state"], mac["lanFailure"], mac["lanFailureAgeSeconds"], mac["name"]),
                         ("linked", None, None, "target-mac"))

    def test_without_a_name_no_publisher_links(self):
        self.assertEqual(self.run_with(None, ["only-mac"])["state"], "unreachable")
        self.assertEqual(self.run_with(None, ["mac-a", "mac-b"])["state"], "unreachable")
