"""pair_status reads NVIDIA PAIR's files read-only: an allowlist, never node.key, a bounded log tail,
and unknown (never down or 0) whenever the files cannot prove otherwise."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import pair_status

KEY_SENTINEL = "SENTINEL-PRIVATE-KEY-MATERIAL-7f3a"
CERT_SENTINEL = "SENTINEL-CERTIFICATE-9c1d"
TRUST_SENTINEL = "SENTINEL-TRUSTED-PEER-2b8e"
MAC_UUID = "11111111-1111-4111-8111-111111111111"
PC_UUID = "22222222-2222-4222-8222-222222222222"
LINUX_UUID = "33333333-3333-4333-8333-333333333333"
HOST_LINUX = "44444444-4444-4444-8444-444444444444"
HOST_PC = "55555555-5555-4555-8555-555555555555"
CLUSTER_ID = "66666666-6666-4666-8666-666666666666"
ADDRESSES = ("10.99.0.176", "10.99.0.71", "10.99.0.200", "10.99.0.95")


def iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + ".123Z"


def log_line(at, message, data=None, level="info"):
    return json.dumps({"level": level, "time": iso(at), "source": "service-bridge",
                       "sublevel": "engine-manager", "message": message, "data": data or {}})


def discovery(now):
    return [
        {"id": "mac-host.local", "name": "mac-host.local", "hostUuid": "77777777-7777-4777-8777-777777777777",
         "ipAddress": ADDRESSES[0], "ipAddresses": [ADDRESSES[0]], "port": 14318, "lastSeen": int(now - 30),
         "trusted": True, "clustered": True, "models": ["qwen/a", "gemma/b"],
         "modelsByEngine": {"lmstudio": ["qwen/a", "gemma/b"], "llamacpp": []},
         "loadedByEngine": {"lmstudio": ["qwen/a"], "llamacpp": []}},
        {"id": "WinPc01", "name": "WinPc01", "hostUuid": HOST_PC, "ipAddress": ADDRESSES[1],
         "port": 14318, "lastSeen": int(now - 4 * 3600), "trusted": True, "clustered": True,
         "modelsByEngine": {"llamacpp": [], "ollama": []}, "loadedByEngine": {"llamacpp": [], "ollama": []}},
        {"id": "linux-node-1", "name": "linux-node-1", "hostUuid": HOST_LINUX, "ipAddress": ADDRESSES[2],
         "ipAddresses": [ADDRESSES[2], ADDRESSES[3]], "port": 14318, "lastSeen": int(now - 1),
         "trusted": True, "clustered": True, "modelsByEngine": {"llamacpp": ["qwen35-0.5b", "coder-1.5b"]},
         "loadedByEngine": {"llamacpp": []}},
    ]


ENGINES = [{"engine": "llamacpp", "display_name": "llama.cpp", "installed": True, "running": True,
            "healthy": True, "port": 8081},
           {"engine": "lmstudio", "display_name": "LM Studio", "installed": True, "running": False,
            "healthy": False, "port": 1235}]


class Fixture:
    """A PAIR data folder shaped like PAIR 1.0.0.85's, plus key material that must never be read."""

    def __init__(self, root: Path, now: float, log_age: float = 3.0):
        self.root, self.now = root, now
        (root / "cluster/trusted").mkdir(parents=True)
        (root / "logs").mkdir()
        (root / "cluster/node.key").write_text(KEY_SENTINEL)
        (root / "cluster/node.crt").write_text(CERT_SENTINEL)
        (root / "cluster/trusted/peer.json").write_text(json.dumps({"cert": TRUST_SENTINEL}))
        (root / "cluster/identity.json").write_text(json.dumps({"secret": KEY_SENTINEL}))
        (root / "node-id.json").write_text(json.dumps({"id": KEY_SENTINEL}))
        self.write("settings.json", {"force_ports": True, "cluster_auto_sync": False, "cluster_id": CLUSTER_ID,
                                     "cluster_friendly_name": "linux-node-1"})
        self.write("cluster/admission.json", {"counter": 3, "activated": 1, "clusterId": CLUSTER_ID, "epoch": 2})
        self.write("cluster/members.json", [
            {"id": "mac-host.local", "nodeUuid": MAC_UUID, "name": "mac-host.local", "ipAddress": ADDRESSES[0],
             "port": 14321, "clusterId": CLUSTER_ID, "admissionEpoch": 2, "state": "member",
             "joinedAt": int(now * 1000) - 10, "lastSeen": None},
            {"id": "WinPc01", "nodeUuid": PC_UUID, "name": "WinPc01", "ipAddress": ADDRESSES[1],
             "port": 14321, "clusterId": CLUSTER_ID, "admissionEpoch": 2, "state": "member",
             "joinedAt": int(now * 1000) - 10, "lastSeen": None},
            {"id": "linux-node-1", "nodeUuid": LINUX_UUID, "name": "linux-node-1", "ipAddress": ADDRESSES[2],
             "port": 14321, "clusterId": CLUSTER_ID, "admissionEpoch": 2, "state": "member",
             "joinedAt": int(now * 1000) - 10, "lastSeen": None}])
        ms = int((now - 7200) * 1000)
        workloads = ([{"id": str(i), "engine": "lmstudio", "model": "qwen/a", "state": "completed", "error": None,
                       "originatedFrom": MAC_UUID, "scheduledOn": MAC_UUID, "createdAt": ms, "startedAt": ms,
                       "completedAt": ms + 500, "runId": "r", "seq": 4, "requesterId": None} for i in range(5)]
                     + [{"id": "f", "engine": "llamacpp", "model": "coder", "state": "failed", "error": "boom",
                         "originatedFrom": MAC_UUID, "scheduledOn": LINUX_UUID, "createdAt": ms, "startedAt": ms,
                         "completedAt": ms + 100, "runId": "r", "seq": 5, "requesterId": None}])
        self.write("workloads-history.json", workloads)
        old = now - 3600
        os.utime(root / "workloads-history.json", (old, old))
        self.lines = [
            log_line(now - 400, "discovery:nodes-changed", discovery(now - 400), level="verbose"),
            log_line(now - 20, "discovery:nodes-changed", discovery(now), level="verbose"),
            log_line(now - 10, f"remote engine status polled for {HOST_LINUX}", {"engines": ENGINES}),
            log_line(now - 9, f"remote engine status polled for {HOST_PC}", {"engines": ENGINES}),
            log_line(now - 8, "remote engine status polled for 99999999-9999-4999-8999-999999999999",
                     {"engines": ENGINES}),
            log_line(now - log_age, "remote engine status sweep: 2 peer(s)", {"peerIds": [HOST_LINUX, HOST_PC]}),
        ]
        self.write_log()

    def write(self, rel, value):
        (self.root / rel).write_text(json.dumps(value))

    def write_log(self, prefix=b""):
        (self.root / "logs/nvpair.jsonl").write_bytes(prefix + ("\n".join(self.lines) + "\n").encode())


class PairStatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agiw-pair-status-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Personal AI Router"
        self.root.mkdir()
        self.now = float(int(time.time()))
        self.fixture = Fixture(self.root, self.now)

    def read(self, **kwargs):
        return pair_status.read_pair_status(self.root, now=kwargs.pop("now", self.now))

    def test_reads_only_the_allowlist_and_never_key_material(self):
        opened = []
        real_open = os.open

        def recording_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            parent = directories.get(kwargs.get("dir_fd"), self.root.parent)
            absolute = parent / path
            if flags & os.O_DIRECTORY:
                self.assertTrue(flags & os.O_NOFOLLOW)
                directories[descriptor] = absolute
            else:
                self.assertEqual(Path(path).name, str(path))
                self.assertIn(kwargs.get("dir_fd"), directories)
                opened.append(str(absolute))
            return descriptor
        directories = {}
        with mock.patch.object(pair_status.os, "open", side_effect=recording_open):
            doc = self.read()
        relative = {os.path.relpath(path, self.root) for path in opened}
        self.assertEqual(relative, set(pair_status.ALLOWED))
        dumped = json.dumps(doc)
        for sentinel in (KEY_SENTINEL, CERT_SENTINEL, TRUST_SENTINEL):
            self.assertNotIn(sentinel, dumped)
        for forbidden in ("node.key", "node.crt", "trusted", "identity.json", "node-id.json"):
            self.assertFalse(any(forbidden in path for path in opened), forbidden)

    def test_forbidden_or_unlisted_names_cannot_be_requested(self):
        for rel in ("cluster/node.key", "cluster/node.crt", "cluster/../cluster/node.key",
                    "cluster/trusted/peer.json", "node-id.json", "/etc/passwd"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                pair_status._read(self.root, rel, 4096)

    def test_symlink_in_place_of_an_allowed_file_is_refused(self):
        members = self.root / "cluster/members.json"
        members.unlink()
        members.symlink_to(self.root / "cluster/node.key")
        doc = self.read()
        self.assertEqual(doc["files"]["cluster/members.json"], "symlink")
        self.assertIsNone(doc["cluster"]["members"])
        self.assertIsNone(doc["cluster"]["memberCount"])
        self.assertNotIn(KEY_SENTINEL, json.dumps(doc))

    def test_symlinked_directory_is_refused(self):
        elsewhere = self.root.parent / "elsewhere"
        (self.root / "cluster").rename(elsewhere)
        (self.root / "cluster").symlink_to(elsewhere, target_is_directory=True)
        doc = self.read()
        self.assertEqual(doc["files"]["cluster/members.json"], "symlink")
        self.assertEqual(doc["files"]["cluster/admission.json"], "symlink")
        self.assertIsNone(doc["cluster"]["epoch"])

    def _assert_directory_replacement_does_not_redirect(self, directory, rel, replacement):
        original = self.root / directory if directory else self.root
        displaced = self.root.parent / "displaced"
        elsewhere = self.root.parent / "replacement"
        elsewhere.mkdir()
        (elsewhere / Path(rel).name).write_text(replacement)
        real_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path).name == Path(rel).name and not swapped:
                original.rename(displaced)
                original.symlink_to(elsewhere, target_is_directory=True)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(pair_status.os, "open", side_effect=swapping_open):
            doc = self.read()
        self.assertTrue(swapped, "the regression must exercise the directory replacement")
        self.assertNotIn("redirected", json.dumps(doc))
        return doc

    def test_root_replacement_before_file_open_keeps_original_directory(self):
        doc = self._assert_directory_replacement_does_not_redirect(
            "", "settings.json", '{"cluster_friendly_name":"redirected"}')
        self.assertEqual(doc["cluster"]["name"], "linux-node-1")

    def test_cluster_replacement_before_file_open_keeps_original_directory(self):
        doc = self._assert_directory_replacement_does_not_redirect(
            "cluster", "cluster/members.json", '[{"name":"redirected"}]')
        self.assertEqual(doc["cluster"]["memberCount"], 3)

    def test_logs_replacement_before_file_open_keeps_original_directory(self):
        replacement = log_line(self.now - 1, "discovery:nodes-changed", [{"name": "redirected"}])
        doc = self._assert_directory_replacement_does_not_redirect("logs", "logs/nvpair.jsonl", replacement)
        self.assertEqual(len(doc["nodes"]), 3)

    def test_remote_engine_ports_have_no_local_port_claim(self):
        for age in (1, 86400):
            with self.subTest(age=age):
                self.fixture.lines = [log_line(self.now - age,
                    f"remote engine status polled for {HOST_PC}", {"engines": ENGINES})]
                self.fixture.write_log()
                doc = self.read()
                self.assertEqual(doc["pairSays"]["engines"][0]["engines"][1]["port"], 1235)
                self.assertFalse(hasattr(pair_status, "lmstudio_port_claim"),
                                 "remote engine reports must not supply a native LM Studio port")

    def test_stale_node_residency_is_unknown_not_zero(self):
        nodes = discovery(self.now)
        for node in nodes:
            node["lastSeen"] = int(self.now - 31)
        self.fixture.lines = [log_line(self.now - 1, "discovery:nodes-changed", nodes)]
        self.fixture.write_log()
        for node in self.read()["nodes"]:
            with self.subTest(node=node["name"]):
                self.assertIsNone(node["pairClaimLoadedCount"])
                self.assertIsNone(node["pairClaimLoadedModels"])
                self.assertIsNone(node["pairClaimModelCount"])
                self.assertEqual(node["residencyReason"], "stale-last-seen")

    def test_unknown_node_last_seen_has_unknown_residency(self):
        for last_seen in (None, "invalid", int(self.now + 3600)):
            with self.subTest(last_seen=last_seen):
                node = discovery(self.now)[2]
                node["lastSeen"] = last_seen
                self.fixture.lines = [log_line(self.now - 1, "discovery:nodes-changed", [node])]
                self.fixture.write_log()
                result = self.read()["nodes"][0]
                self.assertIsNone(result["lastSeenAgeSeconds"])
                self.assertIsNone(result["pairClaimLoadedCount"])
                self.assertIsNone(result["pairClaimLoadedModels"])
                self.assertIsNone(result["pairClaimModelCount"])
                self.assertEqual(result["residencyReason"],
                                 "future-last-seen" if isinstance(last_seen, int) else "unknown-last-seen")

    def test_node_freshness_uses_unrounded_age_at_thirty_second_boundary(self):
        node = discovery(self.now)[0]
        node["lastSeen"] = int(self.now - 30)
        self.fixture.lines = [log_line(self.now - 1, "discovery:nodes-changed", [node])]
        self.fixture.write_log()
        for offset, expected in ((0, (2, 1, ["qwen/a"], None)),
                                 (0.001, (None, None, None, "stale-last-seen"))):
            with self.subTest(offset=offset):
                result = self.read(now=self.now + offset)["nodes"][0]
                self.assertEqual(result["lastSeenAgeSeconds"], 30.0)
                self.assertEqual((result["pairClaimModelCount"], result["pairClaimLoadedCount"], result["pairClaimLoadedModels"],
                                  result["residencyReason"]), expected)

    def test_invalid_history_without_logs_cannot_mark_pair_up(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        for data in (b"not json", b'{}', b'["not a row"]',
                     b"[" + b"{}," * pair_status.MAX_WORKLOAD_ROWS + b"{}]"):
            with self.subTest(size=len(data)):
                (self.root / "workloads-history.json").write_bytes(data)
                os.utime(self.root / "workloads-history.json", (self.now - 1, self.now - 1))
                doc = self.read()
                self.assertEqual(doc["files"]["workloads-history.json"], "invalid")
                self.assertIsNone(doc["workloads"])
                self.assertIsNone(doc["evidence"]["historyAgeSeconds"])
                self.assertEqual((doc["state"], doc["reason"]), ("unknown", "no-evidence"))

    def test_oversized_history_without_logs_cannot_mark_pair_up(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        (self.root / "workloads-history.json").write_bytes(b" " * (pair_status.MAX_WORKLOADS + 1))
        os.utime(self.root / "workloads-history.json", (self.now - 1, self.now - 1))
        doc = self.read()
        self.assertEqual(doc["files"]["workloads-history.json"], "too-large")
        self.assertIsNone(doc["workloads"])
        self.assertIsNone(doc["evidence"]["historyAgeSeconds"])
        self.assertEqual((doc["state"], doc["reason"]), ("unknown", "no-evidence"))

    def test_incomplete_workload_rows_without_logs_cannot_mark_pair_up(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        valid = {"state": "completed", "engine": "lmstudio", "createdAt": int(self.now - 10) * 1000}
        rows = [{}, {k: v for k, v in valid.items() if k != "state"}, dict(valid, state=""),
                {k: v for k, v in valid.items() if k != "engine"}, dict(valid, engine="bad engine"),
                {k: v for k, v in valid.items() if k != "createdAt"}, dict(valid, createdAt="invalid"),
                dict(valid, createdAt=0), dict(valid, createdAt=True)]
        for row in rows:
            with self.subTest(row=row):
                self.fixture.write("workloads-history.json", [row])
                os.utime(self.root / "workloads-history.json", (self.now - 1, self.now - 1))
                doc = self.read()
                self.assertEqual(doc["files"]["workloads-history.json"], "invalid")
                self.assertIsNone(doc["workloads"])
                self.assertIsNone(doc["evidence"]["historyAgeSeconds"])
                self.assertEqual((doc["state"], doc["reason"]), ("unknown", "no-evidence"))

    def test_fresh_empty_history_without_logs_is_valid(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        self.fixture.write("workloads-history.json", [])
        os.utime(self.root / "workloads-history.json", (self.now - 1, self.now - 1))
        doc = self.read()
        self.assertEqual(doc["files"]["workloads-history.json"], "ok")
        self.assertEqual(doc["workloads"]["total"], 0)
        self.assertEqual((doc["state"], doc["reason"], doc["evidence"]["historyAgeSeconds"]),
                         ("up", None, 1.0))

    def test_live_fixture_is_up_with_members_nodes_and_workloads(self):
        doc = self.read()
        self.assertEqual((doc["state"], doc["reason"], doc["installed"]), ("up", None, True))
        self.assertAlmostEqual(doc["evidence"]["logAgeSeconds"], 3.0, delta=1.0)
        self.assertEqual(doc["cluster"], {
            "name": "linux-node-1", "epoch": 2, "memberCount": 3,
            "members": [{"name": "mac-host.local", "state": "member", "admissionEpoch": 2},
                        {"name": "WinPc01", "state": "member", "admissionEpoch": 2},
                        {"name": "linux-node-1", "state": "member", "admissionEpoch": 2}]})
        nodes = {node["name"]: node for node in doc["nodes"]}
        self.assertAlmostEqual(nodes["linux-node-1"]["lastSeenAgeSeconds"], 1.0, delta=1.0)
        self.assertGreater(nodes["WinPc01"]["lastSeenAgeSeconds"], 3 * 3600)
        self.assertEqual((nodes["mac-host.local"]["pairClaimModelCount"], nodes["mac-host.local"]["pairClaimLoadedCount"],
                          nodes["mac-host.local"]["pairClaimLoadedModels"]), (2, 1, ["qwen/a"]))
        self.assertAlmostEqual(doc["discoveryAgeSeconds"], 20.0, delta=1.0)  # the newest discovery entry wins
        self.assertEqual(doc["workloads"]["total"], 6)
        self.assertEqual(doc["workloads"]["byState"], {"completed": 5, "failed": 1})
        # The PC ran nothing: an explicit 0 from a complete history, not a missing row.
        self.assertEqual(doc["workloads"]["byNode"], {"mac-host.local": 5, "WinPc01": 0, "linux-node-1": 1})
        self.assertEqual(set(doc["files"].values()), {"ok"})

    def test_engine_flags_are_only_pair_says_and_carry_no_addresses_or_ids(self):
        doc = self.read()
        self.assertNotIn("engines", {k for k in doc if k != "pairSays"})
        reports = {report["node"]: report for report in doc["pairSays"]["engines"]}
        self.assertEqual(set(reports), {"linux-node-1", "WinPc01", "unidentified peer"})
        self.assertEqual(reports["linux-node-1"]["engines"][1],
                         {"engine": "lmstudio", "installed": True, "running": False, "healthy": False, "port": 1235})
        self.assertIn("not verified", doc["pairSays"]["note"])
        dumped = json.dumps(doc)
        for value in ADDRESSES + (MAC_UUID, PC_UUID, LINUX_UUID, HOST_LINUX, HOST_PC, CLUSTER_ID):
            self.assertNotIn(value, dumped)

    def test_node_residency_and_engine_inventory_are_explicit_pair_claims(self):
        node = next(row for row in self.read()["nodes"] if row["name"] == "linux-node-1")
        self.assertEqual(node["pairClaimModelCount"], 2)
        self.assertEqual(node["pairClaimLoadedCount"], 0)
        self.assertEqual(node["pairClaimLoadedModels"], [])
        self.assertEqual(node["pairClaimEngines"], ["llamacpp"])
        for key in ("modelCount", "loadedCount", "loadedModels", "engines"):
            self.assertNotIn(key, node)

    def test_cached_file_readings_recompute_ages_and_expire_node_claims_without_file_io(self):
        readings = pair_status.read_pair_files(self.root)
        with mock.patch.object(pair_status, "_open_directory", side_effect=AssertionError("no new file reads")):
            first = pair_status.read_pair_status(now=self.now, readings=readings)
            later = pair_status.read_pair_status(now=self.now + 0.001, readings=readings)
            expired = pair_status.read_pair_status(now=self.now + 1000, readings=readings)
        self.assertEqual(first["nodes"][0]["pairClaimLoadedCount"], 1)
        self.assertIsNone(later["nodes"][0]["pairClaimLoadedCount"])
        self.assertEqual(later["nodes"][0]["residencyReason"], "stale-last-seen")
        self.assertEqual(first["nodes"][0]["lastSeenAgeSeconds"], later["nodes"][0]["lastSeenAgeSeconds"],
                         "rounded display ages must not extend the precise residency deadline")
        self.assertEqual(expired["state"], "unknown")
        self.assertEqual(expired["evidence"]["logAgeSeconds"], first["evidence"]["logAgeSeconds"] + 1000)
        self.assertEqual(expired["discoveryAgeSeconds"], first["discoveryAgeSeconds"] + 1000)
        self.assertEqual(expired["pairSays"]["engines"][0]["reportedAgeSeconds"],
                         first["pairSays"]["engines"][0]["reportedAgeSeconds"] + 1000)
        self.assertEqual(first["state"], "up", "reprojection must not mutate previous public snapshots")

    def test_conflicting_lmstudio_ports_remain_separate_peer_claims(self):
        other = [dict(ENGINES[1], port=1236)]
        self.fixture.lines.append(log_line(self.now - 2, f"remote engine status polled for {HOST_PC}",
                                           {"engines": other}))
        self.fixture.write_log()
        reports = self.read()["pairSays"]["engines"]
        ports = {row["node"]: next(engine["port"] for engine in row["engines"]
                                  if engine["engine"] == "lmstudio") for row in reports}
        self.assertEqual(ports, {"WinPc01": 1236, "linux-node-1": 1235, "unidentified peer": 1235})

    def test_tail_is_bounded_and_drops_the_partial_first_line(self):
        filler = (log_line(self.now - 99999, "old entry") + "\n").encode() * 20000  # several MiB
        self.fixture.write_log(prefix=filler)
        self.assertGreater((self.root / "logs/nvpair.jsonl").stat().st_size, 20 * pair_status.TAIL_BYTES)
        sizes = []
        real_pread = os.pread

        def recording_pread(fd, size, offset):
            sizes.append(size)
            return real_pread(fd, size, offset)
        with mock.patch.object(pair_status.os, "pread", side_effect=recording_pread):
            doc = self.read()
        self.assertLessEqual(max(sizes), pair_status.MAX_WORKLOADS + 1)
        self.assertIn(pair_status.TAIL_BYTES, sizes)
        self.assertEqual(doc["tail"]["bytes"], pair_status.TAIL_BYTES)
        self.assertEqual(doc["tail"]["skipped"], 0)  # the cut first line is dropped, not parsed
        self.assertEqual(doc["state"], "up")

    def test_garbage_lines_are_skipped_not_fatal(self):
        self.fixture.lines.insert(0, "{not json")
        self.fixture.lines.insert(1, "[1, 2]")
        self.fixture.write_log()
        doc = self.read()
        self.assertEqual(doc["tail"]["skipped"], 2)
        self.assertEqual(doc["state"], "up")

    def test_log_timestamp_without_a_valid_event_cannot_mark_pair_up(self):
        for missing_history in (False, True):
            if missing_history:
                (self.root / "workloads-history.json").unlink()
            for message in (None, "", "  \t", 1, [], {}):
                with self.subTest(missing_history=missing_history, message=message):
                    entry = {"time": iso(self.now - 1)}
                    if message is not None:
                        entry["message"] = message
                    self.fixture.lines = [json.dumps(entry)]
                    self.fixture.write_log()
                    doc = self.read()
                    self.assertEqual(doc["state"], "unknown")
                    self.assertEqual(doc["reason"], "no-evidence" if missing_history else "stale")
                    self.assertIsNone(doc["evidence"]["logAgeSeconds"])
                    self.assertEqual((doc["tail"]["lines"], doc["tail"]["skipped"]), (0, 1))

    def test_minimal_log_event_can_supply_freshness(self):
        self.fixture.lines = [json.dumps({"time": iso(self.now - 1), "message": "engine sweep"})]
        self.fixture.write_log()
        self.assertEqual(self.read()["state"], "up")

    def test_log_freshness_uses_unrounded_age_at_cutoff(self):
        at = self.now - pair_status.LOG_FRESH_SECONDS
        self.fixture.lines = [json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)),
                                         "message": "engine sweep"})]
        self.fixture.write_log()
        for offset, state in ((0, "up"), (0.04, "unknown")):
            with self.subTest(offset=offset):
                doc = self.read(now=self.now + offset)
                self.assertEqual(doc["evidence"]["logAgeSeconds"], pair_status.LOG_FRESH_SECONDS)
                self.assertEqual(doc["state"], state)

    def test_history_freshness_uses_unrounded_age_at_cutoff(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        at = self.now - pair_status.HISTORY_FRESH_SECONDS
        os.utime(self.root / "workloads-history.json", (at, at))
        for offset, state in ((0, "up"), (0.04, "unknown")):
            with self.subTest(offset=offset):
                doc = self.read(now=self.now + offset)
                self.assertEqual(doc["evidence"]["historyAgeSeconds"], pair_status.HISTORY_FRESH_SECONDS)
                self.assertEqual(doc["state"], state)

    def test_malformed_members_are_incomplete_not_an_exact_count(self):
        valid = json.loads((self.root / "cluster/members.json").read_text())[0]
        for rows in ([None], [valid, None], [{}], [dict(valid, name="")],
                     [dict(valid, state=None)], [dict(valid, admissionEpoch=True)],
                     [dict(valid, nodeUuid="not-a-uuid")]):
            with self.subTest(rows=rows):
                self.fixture.write("cluster/members.json", rows)
                doc = self.read()
                self.assertIsNone(doc["cluster"]["memberCount"])
                self.assertIsNone(doc["cluster"]["members"])
                self.assertEqual(doc["files"]["cluster/members.json"], "invalid")

    def test_member_limit_cannot_be_reported_as_an_exact_count(self):
        member = json.loads((self.root / "cluster/members.json").read_text())[0]
        for count in (pair_status.MAX_MEMBERS, pair_status.MAX_MEMBERS + 1):
            with self.subTest(count=count):
                self.fixture.write("cluster/members.json", [dict(member, nodeUuid=f"{i:08x}" + MAC_UUID[8:])
                                                            for i in range(count)])
                doc = self.read()
                if count <= pair_status.MAX_MEMBERS:
                    self.assertEqual(doc["cluster"]["memberCount"], count)
                    self.assertEqual(doc["files"]["cluster/members.json"], "ok")
                else:
                    self.assertIsNone(doc["cluster"]["memberCount"])
                    self.assertIsNone(doc["cluster"]["members"])
                    self.assertEqual(doc["files"]["cluster/members.json"], "too-many-members")

    def test_empty_member_list_is_a_known_zero(self):
        self.fixture.write("cluster/members.json", [])
        doc = self.read()
        self.assertEqual(doc["cluster"]["memberCount"], 0)
        self.assertEqual(doc["cluster"]["members"], [])
        self.assertEqual(doc["files"]["cluster/members.json"], "ok")

    def test_missing_folder_is_unknown_never_down(self):
        doc = pair_status.read_pair_status(self.root.parent / "absent", now=self.now)
        self.assertEqual((doc["state"], doc["reason"], doc["installed"]), ("unknown", "not-found", False))
        self.assertIsNone(doc["nodes"])
        self.assertIsNone(doc["workloads"])
        self.assertNotIn('"down"', json.dumps(doc))
        self.assertEqual(pair_status.source_row(doc)["state"], "unavailable")

    def test_empty_folder_gives_unknown_values_not_zero(self):
        empty = self.root.parent / "empty"
        empty.mkdir()
        doc = pair_status.read_pair_status(empty, now=self.now)
        self.assertEqual((doc["state"], doc["reason"], doc["installed"]), ("unknown", "no-evidence", True))
        self.assertEqual(set(doc["files"].values()), {"missing"})
        self.assertEqual(doc["cluster"], {"name": None, "epoch": None, "memberCount": None, "members": None})
        self.assertIsNone(doc["workloads"])
        self.assertIsNone(doc["nodes"])
        self.assertEqual(doc["pairSays"]["engines"], [])

    def test_stale_log_and_history_are_unknown_with_reason(self):
        doc = self.read(now=self.now + 1000)
        self.assertEqual((doc["state"], doc["reason"]), ("unknown", "stale"))
        self.assertIn("not down", doc["detail"])

    def test_fresh_history_alone_counts_as_up(self):
        (self.root / "logs/nvpair.jsonl").unlink()
        os.utime(self.root / "workloads-history.json", (self.now - 60, self.now - 60))
        doc = self.read()
        self.assertEqual(doc["files"]["logs/nvpair.jsonl"], "missing")
        self.assertEqual((doc["state"], doc["evidence"]["historyAgeSeconds"]), ("up", 60.0))

    def test_future_timestamps_are_not_freshness(self):
        self.fixture.lines = [log_line(self.now + 3600, "remote engine status sweep: 2 peer(s)")]
        self.fixture.write_log()
        doc = self.read()
        self.assertIsNone(doc["evidence"]["logAgeSeconds"])
        self.assertEqual(doc["state"], "unknown")

    def test_malformed_and_oversized_files_are_invalid_not_zero(self):
        (self.root / "cluster/members.json").write_text("not json")
        (self.root / "settings.json").write_text(" " * (pair_status.MAX_SMALL + 1))
        self.fixture.write("workloads-history.json", [{"state": "completed"}, "not a row"])
        doc = self.read()
        self.assertEqual(doc["files"]["cluster/members.json"], "invalid")
        self.assertEqual(doc["files"]["settings.json"], "too-large")
        self.assertEqual(doc["files"]["workloads-history.json"], "invalid")
        self.assertIsNone(doc["cluster"]["memberCount"])
        self.assertIsNone(doc["cluster"]["name"])
        self.assertIsNone(doc["workloads"])

    def test_unsafe_model_names_make_the_loaded_list_unknown(self):
        nodes = discovery(self.now)
        nodes[0]["loadedByEngine"] = {"lmstudio": ["qwen/a", "bad name\nwith newline"]}
        self.fixture.lines.append(log_line(self.now - 1, "discovery:nodes-changed", nodes))
        self.fixture.write_log()
        mac = next(node for node in self.read()["nodes"] if node["name"] == "mac-host.local")
        self.assertEqual(mac["pairClaimLoadedCount"], 2)
        self.assertIsNone(mac["pairClaimLoadedModels"])

    def test_source_row_states(self):
        self.assertEqual(pair_status.source_row(self.read())["state"], "live")
        stale = pair_status.source_row(self.read(now=self.now + 1000))
        self.assertEqual(stale["state"], "unavailable")
        self.assertNotEqual(stale["state"], "error")


if __name__ == "__main__":
    unittest.main()
