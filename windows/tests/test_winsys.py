"""Lane process and caller readings with a fake TCP table (runs on any OS)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import winsys  # noqa: E402

LISTEN, ESTAB = winsys.MIB_TCP_STATE_LISTEN, winsys.MIB_TCP_STATE_ESTAB


def row(state, local, lport, remote, rport, pid):
    return {"state": state, "local": local, "localPort": lport, "remote": remote, "remotePort": rport, "pid": pid}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class LaneProcessesTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cpu = {11: 0, 12: 0}
        self.created = {11: 1, 12: 2, 21: 3, 22: 4, 99: 5}
        names = {11: "llama-server.exe", 12: "llama-server.exe", 21: "codex.exe", 22: "python.exe", 99: "python.exe"}
        self.table = [row(LISTEN, "127.0.0.1", 1234, "0.0.0.0", 0, 11), row(LISTEN, "127.0.0.1", 1235, "0.0.0.0", 0, 12),
                      row(ESTAB, "127.0.0.1", 50001, "127.0.0.1", 1234, 21), row(ESTAB, "127.0.0.1", 50002, "127.0.0.1", 1234, 21),
                      row(ESTAB, "127.0.0.1", 50003, "127.0.0.1", 1235, 22),
                      row(ESTAB, "127.0.0.1", 1234, "127.0.0.1", 50001, 11),  # server side of the same connection
                      row(ESTAB, "127.0.0.1", 50009, "127.0.0.1", 1235, 99),  # the observer itself
                      row(ESTAB, "10.0.0.71", 50010, "10.0.0.194", 1234, 22)]  # a LAN connection, not a local caller
        self.lp = winsys.LaneProcesses(table_fn=lambda: self.table, clock=self.clock, cpus=4, self_pid=99,
                                       info_fn=lambda pid: {"pid": pid, "name": names[pid], "created": self.created[pid],
                                                            "cpuTime": self.cpu.get(pid, 0), "workingSetBytes": 16 * 2**30})

    def test_servers_callers_and_cpu_share(self):
        servers, callers, source = self.lp.sample({1234: "deep", 1235: "fast"})
        self.assertEqual(servers["deep"]["pid"], 11)
        self.assertIsNone(servers["deep"]["cpuPercent"])  # needs two samples
        self.clock.now += 2.0
        self.cpu[11] = 4 * 10_000_000  # 4 CPU-seconds over 2 s on 4 CPUs = 50 %
        servers, callers, source = self.lp.sample({1234: "deep", 1235: "fast"})
        self.assertEqual(servers["deep"]["cpuPercent"], 50.0)
        self.assertEqual(servers["fast"]["cpuPercent"], 0.0)
        self.assertEqual([(c["name"], c["lane"], c["client"], c["connections"]) for c in callers],
                         [("codex.exe", "deep", "codex", 2), ("python.exe", "fast", None, 1)])
        self.assertEqual(source["state"], "live")
        self.assertIn("codex.exe -> deep", source["detail"])

    def test_restarted_lane_does_not_inherit_counters(self):
        self.lp.sample({1234: "deep"})
        self.clock.now += 2.0
        self.created[11] = 777  # same pid, new process
        self.cpu[11] = 10**12
        servers, _, _ = self.lp.sample({1234: "deep"})
        self.assertIsNone(servers["deep"]["cpuPercent"])

    def test_connected_time_accumulates_and_resets(self):
        self.lp.sample({1234: "deep"})
        self.clock.now += 5.0
        _, callers, _ = self.lp.sample({1234: "deep"})
        self.assertEqual(callers[0]["connectedSeconds"], 5.0)
        self.table = [r for r in self.table if r["pid"] != 21]
        self.lp.sample({1234: "deep"})
        self.table.append(row(ESTAB, "127.0.0.1", 50011, "127.0.0.1", 1234, 21))
        _, callers, _ = self.lp.sample({1234: "deep"})
        self.assertEqual(callers[0]["connectedSeconds"], 0.0)

    def test_unreadable_table_is_a_source_not_a_crash(self):
        def broken():
            raise OSError("no")
        lp = winsys.LaneProcesses(table_fn=broken)
        servers, callers, source = lp.sample({1234: "deep"})
        self.assertEqual((servers, callers, source["state"]), ({}, [], "unavailable"))

    def test_client_mapping(self):
        self.assertEqual(winsys.client_for("Codex.exe"), "codex")
        self.assertEqual(winsys.client_for("opencode-cli.exe"), "opencode")
        self.assertIsNone(winsys.client_for("node.exe"))


if __name__ == "__main__":
    unittest.main()
